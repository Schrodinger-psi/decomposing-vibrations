from __future__ import annotations

import itertools
from typing import NamedTuple
from typing import Iterable
import pprint
import string
import os
import numpy as np
import pandas as pd
import argparse
import logging
import time
import pymatgen.core as mg
from pymatgen.symmetry.analyzer import PointGroupAnalyzer
from mendeleev.fetch import fetch_table


import icgen
import icsel
import bmatrix
import logfile
import molpro_parser
import specifications

class Atom(NamedTuple):
    symbol: str
    coordinates: tuple

#TODO: isotopes for all elements with command line input
def get_mass_information() -> pd.DataFrame:
    df = fetch_table('elements')
    mass_info = df.loc[:, ['symbol', 'atomic_weight']]
    deuterium_info = pd.DataFrame({'symbol': ['D'], 'atomic_weight': [2.014102]})
    mass_info = pd.concat([mass_info, deuterium_info])
    mass_info.set_index("symbol", inplace=True)
    return mass_info

def reciprocal_square_massvector(atoms):
    n_atoms = len(atoms)
    diag_reciprocal_square = np.zeros(3*n_atoms)
    MASS_INFO = get_mass_information()
    for i in range(0, n_atoms):
        diag_reciprocal_square[3*i:3*i+3] = 1/np.sqrt(MASS_INFO.loc[atoms[i].symbol.strip(string.digits)])
    return diag_reciprocal_square

def reciprocal_massvector(atoms):
    n_atoms = len(atoms)
    diag_reciprocal = np.zeros(3*n_atoms)
    MASS_INFO = get_mass_information()
    for i in range(0, n_atoms):
        diag_reciprocal[3*i:3*i+3] = 1/(MASS_INFO.loc[atoms[i].symbol.strip(string.digits)])
    return diag_reciprocal

def strip_numbers(string):
    return ''.join([char for char in string if not char.isdigit()])

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    # TODO: look up how to set flag directly
    parser.add_argument("debug")
    args = parser.parse_args()
    return args


start_time = time.time()

def main():
    # Reading Cartesian Coordinates and Hessian
    args = get_args()
    with open(args.output) as inputfile:
        atoms=molpro_parser.parse_xyz_from_inputfile(inputfile)
        n_atoms = len(atoms) 
    with open(args.output) as inputfile:
        CartesianF_Matrix = molpro_parser.parse_Cartesian_F_Matrix_from_inputfile(inputfile) 
        outputfile = logfile.create_new_filename(inputfile.name)

    if args.debug == "debug":
        DEBUG_MODE = True
    else:
        DEBUG_MODE = False

    
    # initialize log file
    if os.path.exists(outputfile):
        i = 1
        while True:
            new_outputfile_name = f"{outputfile}_{i}"
            if not os.path.exists(new_outputfile_name):
                os.rename(outputfile, new_outputfile_name)
                break
            i +=1
   
    logging.basicConfig(filename=outputfile, filemode='a', format='%(message)s', level=logging.DEBUG)
    logfile.write_logfile_header() 
    

    # Determining molecular symmetry
    molecule = mg.Molecule([strip_numbers(atom.symbol) for atom in atoms], [atom.coordinates for atom in atoms])
    molecule_pg = PointGroupAnalyzer(molecule)
    point_group_sch = molecule_pg.sch_symbol

    # Generation of all possible bonding and bending internal coordinates
    bonds = icgen.initialize_bonds(atoms)
    angles, linear_angles = icgen.initialize_angles(atoms)

    # Setting specifications for calculation: check if molecule is linear, planar or a general molecule
    specification = dict()
    specification = specifications.calculation_specification(specification, atoms, molecule_pg, bonds, angles, linear_angles)

    # Generation of all possible out-of-plane motions

    if specification["planar"] == "yes":
        out_of_plane = icgen.initialize_oop(atoms)
    elif specification["planar"] == "no" and specification["planar submolecule(s)"] == []:
        out_of_plane = []
    elif specification["planar"] == "no" and not (specification["planar submolecule(s)"] == []):
        out_of_plane = icgen.initialize_oop_planar_subunits(atoms, specification["planar submolecule(s)"])
    else:
        return logging.error("Classification of whether topology is planar or not could not be determined!")
    dihedrals = icgen.initialize_dihedrals(atoms)

    # determine internal degrees of freedom 
    idof = 0
    if specification["linearity"] == "fully linear":
        idof = 3*n_atoms-5
    else:
        idof = 3*n_atoms-6

   # update log file

    logfile.write_logfile_oop_treatment(specification["planar"], specification["planar submolecule(s)"])
    logfile.write_logfile_symmetry_treatment(specification, point_group_sch)

    # Computation of the diagonal mass matrices with 
    # the reciprocal and square root reciprocal masses
    diag_reciprocal_square = reciprocal_square_massvector(atoms)
    reciprocal_square_massmatrix = np.diag(diag_reciprocal_square)
    diag_reciprocal = reciprocal_massvector(atoms)
    reciprocal_massmatrix = np.diag(diag_reciprocal)
    
    # Determination of the Normal Modes and eigenvalues 
    # via the diagonalization of the mass-weighted Cartesian F Matrix
    
    Mass_weighted_CartesianF_Matrix = np.transpose(reciprocal_square_massmatrix) @ CartesianF_Matrix @ reciprocal_square_massmatrix
    
    Cartesian_eigenvalues, L = np.linalg.eigh(Mass_weighted_CartesianF_Matrix)
    #print("Cartesian_eigenvalues (EV from mw hessian):", Cartesian_eigenvalues)

    # Determination of the normal modes of zero and low Frequencies

    rottra = L[:,0:(3*n_atoms-idof)]
    
    logfile.write_logfile_generated_IC(bonds, angles, linear_angles, out_of_plane, dihedrals, idof)

    ic_dict = icsel.get_sets(idof,atoms, bonds, angles, linear_angles, out_of_plane, dihedrals, specification)
    
    if DEBUG_MODE:
        debug_data = pd.DataFrame(index=range(len(ic_dict.keys())),
                columns=["bonds","angles","linear angles","out-of-plane","dihedrals","red",
                    "complete","imag. intr. freq","sum norm","eucledian norm","maximum norm","MAD", "EV f-matrix", "number of 0 EV f-matrix"])
    

    for num_of_set in ic_dict.keys():
        bonds = ic_dict[num_of_set]["bonds"]
        angles = ic_dict[num_of_set]["angles"]
        linear_angles = ic_dict[num_of_set]["linear valence angles"]
        out_of_plane = ic_dict[num_of_set]["out of plane angles"]
        dihedrals = ic_dict[num_of_set]["dihedrals"]

        n_internals = len(bonds) + len(angles) + len(linear_angles) + len(out_of_plane) + len(dihedrals)
        red = n_internals - idof
        
        # Augmenting the B-Matrix with rottra, calculating 
        # and printing the final B-Matrix

        B = np.concatenate((bmatrix.b_matrix(atoms, bonds, angles, linear_angles, out_of_plane, dihedrals, idof),
                            np.transpose(rottra)),axis=0)

        # Calculating the G-Matrix

        G = B @ reciprocal_massmatrix @ np.transpose(B)
        e,K = np.linalg.eigh(G)

        # Sorting eigenvalues and eigenvectors (just for the case)
        # Sorting highest eigenvalue/eigenvector to lowest!

        idx = e.argsort()[::-1]   
        e = e[idx]
        K = K[:,idx]

        # if redundancies are present, then approximate the inverse of the G-Matrix
        if red > 0:
            K = np.delete(K, -red, axis=1)
            e = np.delete(e, -red, axis=0)

        e = np.diag(e)
        try:
            G_inv = K @ np.linalg.inv(e) @ np.transpose(K)
        except np.linalg.LinAlgError:
            G_inv = K @ np.linalg.pinv(e) @ np.transpose(K)

        # Calculating the inverse augmented B-Matrix

        B_inv = reciprocal_massmatrix @ np.transpose(B) @ G_inv
        InternalF_Matrix = np.transpose(B_inv) @ CartesianF_Matrix @ B_inv

        logfile.write_logfile_information_results(B, B_inv, CartesianF_Matrix, InternalF_Matrix, n_internals, red, bonds, 
        angles, linear_angles, out_of_plane, dihedrals)

        if not DEBUG_MODE and icsel.test_completeness(CartesianF_Matrix, B, B_inv, InternalF_Matrix) != True:
             continue
        elif DEBUG_MODE and icsel.test_completeness(CartesianF_Matrix, B, B_inv, InternalF_Matrix) != True:
            COMPLETE=False
        elif DEBUG_MODE and icsel.test_completeness(CartesianF_Matrix, B, B_inv, InternalF_Matrix) == True:
            COMPLETE=True
            
        ''''' 
        --------------------------- Main-Calculation ------------------------------
        ''''' 

        # Calculation of the mass-weighted normal modes in Cartesian Coordinates

        l = reciprocal_square_massmatrix @ L

        # Calculation of the mass-weighted normal modes in Internal Coordinates

        D = B @ l

        # Calculation of the Vibrational Density Matrices
        
        eigenvalues = np.transpose(D) @ InternalF_Matrix @ D
        eigenvalues = np.diag(eigenvalues)
        # print("eigenvalues (from IC space):", eigenvalues)

        P = np.zeros((n_internals-red,n_internals,n_internals))

        for i in range(0,n_internals-red):
            for m in range(0,n_internals):
                for n in range(0,n_internals):
                    k = i + (3*n_atoms-idof)
                    P[i][m][n] = D[m][k]*InternalF_Matrix[m][n]*D[n][k] / eigenvalues[k]


#small testing for properties of P tensor

#        sum_test = np.zeros((n_internals-red))
#        for i in range(0,n_internals-red):
#            for m in range(0,n_internals):
#                for n in range(0,n_internals):
#                    sum_test[i] += P[i][m][n]
#        print(sum_test)

        ''''' 
        ------------------------------- Results --------------------------------------
        ''''' 

        # Results part 1 
        Diag_elements = np.zeros((n_internals-red,n_internals))
        for i in range(0,n_internals-red):
            for n in range (0,n_internals):
                Diag_elements[i][n] = np.diag(P[i])[n]

        Diag_elements = np.transpose(Diag_elements)

        nu = np.zeros(n_internals) 
        for n in range(0,n_internals):
            for m in range(0,n_internals):
                for i in range(0,n_internals-red):
                    k = i + (3*n_atoms-idof)
                    nu[n] += D[m][k] * InternalF_Matrix[m][n] * D[n][k]
                    
        if not DEBUG_MODE and np.any(nu < 0) == True:
            logfile.write_logfile_nan_freq()
            continue
        elif DEBUG_MODE and np.any(nu < 0) == True:
            logfile.write_logfile_nan_freq_debug()
            nu[nu<0] = 0
            IMAGINARY=True
        elif DEBUG_MODE and np.any(nu >= 0) == True:
            IMAGINARY=False
        
        nu_final = np.sqrt(nu) *  5140.4981

        normal_coord_harmonic_frequencies = np.sqrt(eigenvalues[(3*n_atoms-idof):3*n_atoms]) * 5140.4981
        normal_coord_harmonic_frequencies = np.around(normal_coord_harmonic_frequencies, decimals=2)
        normal_coord_harmonic_frequencies_string = normal_coord_harmonic_frequencies.astype('str')

        all_internals = bonds + angles + linear_angles + out_of_plane + dihedrals

        Results1 = pd.DataFrame()
        Results1['Internal Coordinate'] = all_internals
        Results1['Intrinsic Frequencies'] = pd.DataFrame(nu_final).applymap("{0:.2f}".format)
        Results1 = Results1.join(pd.DataFrame(Diag_elements).applymap("{0:.2f}".format))

        columns = {}
        keys = range(3*n_atoms-((3*n_atoms-idof)))
        for i in keys:
            columns[i] = normal_coord_harmonic_frequencies_string[i]

        Results1 = Results1.rename(columns=columns)

        sum_array1 = np.zeros(n_internals)

        for n in range(0,n_internals):
            for i in range(0, n_internals-red):
                sum_array1[i] += Diag_elements[n][i]

        Contribution_Matrix1 = np.zeros((n_internals, n_internals-red))
        for i in range(0, n_internals-red):
            Contribution_Matrix1[:,i] =((Diag_elements[:,i] / sum_array1[i]) * 100).astype(float)
        Contribution_Table1 = pd.DataFrame()
        Contribution_Table1['Internal Coordinate'] = all_internals
        Contribution_Table1 = Contribution_Table1.join(pd.DataFrame(Contribution_Matrix1).applymap("{0:.2f}".format))
        Contribution_Table1 = Contribution_Table1.rename(columns=columns)


        #MAD is not computed between frequencies but force constants!
        mean_average_deviation = icsel.Kemalian_metric(Contribution_Matrix1/100,nu,eigenvalues[(3*n_atoms-idof):3*n_atoms])

        logfile.write_logfile_results(Results1, Contribution_Table1, mean_average_deviation)
        if DEBUG_MODE:
            eval_double_f = np.round(icsel.check_evalue_f_matrix(reciprocal_square_massmatrix, B, B_inv, InternalF_Matrix),6)
            # freq = np.sqrt(icsel.check_evalue_f_matrix(reciprocal_square_massmatrix, B, B_inv, InternalF_Matrix)) * 5140.4981
            #print("eval_double_f (now mw):", eval_double_f)
            #print(freq)
            debug_data.loc[num_of_set]=[len(bonds), len(angles), len(linear_angles), len(out_of_plane), len(dihedrals),
                    red,COMPLETE,IMAGINARY,np.round(icsel.matrix_norm(B,B_inv,1),2),np.round(icsel.matrix_norm(B,B_inv,2),2),np.round(icsel.matrix_norm(B,B_inv,np.inf),2),
                    np.round(mean_average_deviation,4), eval_double_f, np.count_nonzero(eval_double_f == 0)]

    if DEBUG_MODE:
        print(debug_data)
        csv_name = outputfile.replace("_nomodeco.log","") + "_debug.csv" 
        debug_data.to_csv(csv_name)
    print("Runtime: %s seconds" % (time.time() - start_time))

if __name__ == '__main__':
    main()


        # Results part 2
# To be honest, I have never needed the results here, so I deactivated them
# This is bascially calculation of the eigenvalues PER normal coordinate

#        nu_perNormalCoordinate = np.zeros((n_internals,n_internals-red)) 
#        for n in range(0,n_internals):
#            for i in range(0,n_internals-red):
#                for m in range(0,n_internals):
#                    k = i + (3*n_atoms-idof)
#                    nu_perNormalCoordinate[n][i] += D[m][k] * InternalF_Matrix[m][n] * D[n][k]
#
#        Results2 = pd.DataFrame()
#        Results2['Internal Coordinate'] = all_internals
#        Results2 = Results2.join(pd.DataFrame(nu_perNormalCoordinate).applymap("{0:.2f}".format))
#        Results2 = Results2.rename(columns=columns)
#
#
#        sum_array2 = np.zeros(n_internals)
#
#        for n in range(0,n_internals):
#            for i in range(0,n_internals-red):
#                sum_array2[i] += nu_perNormalCoordinate[n][i]
#
#        Contribution_Matrix2 = np.zeros((n_internals,n_internals-red))
#        for i in range(0, n_internals-red):
#            Contribution_Matrix2[:,i] = (nu_perNormalCoordinate[:,i] / sum_array2[i]) * 100
#
#        Contribution_Table2 = pd.DataFrame()
#        Contribution_Table2['Internal Coordinate'] = all_internals
#        Contribution_Table2 = Contribution_Table2.join(pd.DataFrame(Contribution_Matrix2).applymap("{0:.2f}".format))
#        Contribution_Table2 = Contribution_Table2.rename(columns=columns)
